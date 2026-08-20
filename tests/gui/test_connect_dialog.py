"""The Connect dialog: what it collects, and what it does while it waits."""

from __future__ import annotations

import base64
import json

import pytest

from deepreefmap_gui.server.connect_ui import (
    CONNECT,
    CONNECTING,
    INTRO,
    NAME_NOTE,
    SERVER_LABEL,
    UNREADABLE,
    ConnectDialog,
)
from deepreefmap_gui.sync.connect_code import CODE_PREFIX


def make_code(url: str = "https://reef.example.org") -> str:
    """A pasted code, as the registry's web interface hands one out."""
    payload = json.dumps({"url": url, "code": "ab" * 32})
    return CODE_PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


CODE = make_code()


@pytest.fixture
def dialog(qapp):
    made = ConnectDialog()
    yield made
    made.deleteLater()


def test_connect_waits_for_something_to_be_pasted(dialog):
    assert not dialog._connect_btn.isEnabled()

    dialog._code_edit.setPlainText(CODE)

    assert dialog._connect_btn.isEnabled()


def test_the_server_is_named_before_the_button_works(dialog):
    """An operator pastes a code out of an email, so which host it trusts is shown
    while there is still a chance to not press Connect."""
    dialog._code_edit.setPlainText(make_code("https://reef.epfl.ch"))

    assert dialog._server.isVisibleTo(dialog)
    assert dialog._server.text() == f"{SERVER_LABEL} https://reef.epfl.ch"
    assert dialog._connect_btn.isEnabled()


def test_a_code_that_does_not_decode_never_reaches_the_network(dialog):
    dialog._code_edit.setPlainText("drm1." + "not a code")

    assert dialog._server.text() == UNREADABLE
    assert not dialog._connect_btn.isEnabled()


def test_a_plain_http_server_is_refused_rather_than_warned_about(dialog):
    """The device token crosses that network in the clear, so this is not advice."""
    dialog._code_edit.setPlainText(make_code("http://reef.example.org"))

    assert "http://reef.example.org" in dialog._server.text()
    assert not dialog._connect_btn.isEnabled()


def test_a_loopback_server_over_plain_http_still_connects(dialog):
    """A local stack never leaves the machine, and is how the app is developed."""
    dialog._code_edit.setPlainText(make_code("http://localhost:88"))

    assert dialog._connect_btn.isEnabled()


def test_the_dialog_offers_no_name_to_type(dialog):
    """The device enrols under the machine's own name; naming lives in the web
    interface, so the dialog says so rather than collecting one."""
    assert "machine name" in NAME_NOTE
    assert "not you" in INTRO
    assert not hasattr(dialog, "_name_edit")


def test_pressing_connect_hands_over_the_code(dialog):
    handed: list[str] = []
    dialog.submitted.connect(handed.append)
    dialog._code_edit.setPlainText(f"  {CODE}  ")

    dialog._connect_btn.click()

    assert handed == [CODE]


def test_an_enrolment_in_flight_locks_what_it_is_using(dialog):
    dialog._code_edit.setPlainText(CODE)

    dialog._connect_btn.click()

    assert dialog._connect_btn.text() == CONNECTING
    assert not dialog._connect_btn.isEnabled()
    assert dialog._code_edit.isReadOnly()
    assert dialog._spinner.isVisibleTo(dialog)


def test_a_refusal_leaves_the_code_there_to_be_fixed(dialog):
    """A connect code is a couple of hundred characters, so a failure must not
    throw away what was pasted."""
    dialog._code_edit.setPlainText(CODE)
    dialog._connect_btn.click()

    dialog.show_failure("The connect code was refused", "It has already been used.")

    assert "already been used" in dialog._message.text()
    assert dialog._code_edit.toPlainText() == CODE
    assert not dialog._code_edit.isReadOnly()
    assert dialog._connect_btn.text() == CONNECT
    assert dialog._connect_btn.isEnabled()
    assert not dialog._spinner.isVisibleTo(dialog)
