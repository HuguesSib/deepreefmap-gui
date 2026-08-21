"""The pre-sync disclaimer, pinned to the wire it describes.

Prose about what travels drifts silently unless it is checked against the code
that decides what travels. Each test ties one sentence of the disclaimer to
the constant or function that makes it true, so changing the wire without
rewording the page fails here.
"""

from __future__ import annotations

from deepreefmap_gui.server.state import DISCLAIMER, SECTION_LABELS, heartbeat_report
from deepreefmap_gui.sync.engine import AUTHORED_SECTIONS
from deepreefmap_gui.sync.wire import _DEVICE_LOCAL

TEXT = " ".join(DISCLAIMER)


def test_every_pushed_record_kind_is_named_in_the_apps_own_words():
    for section in AUTHORED_SECTIONS:
        assert SECTION_LABELS[section].lower() in TEXT.lower()


def test_the_footage_claim_names_the_button_that_sends_it():
    from deepreefmap_gui.server.page_ui import ARCHIVE_NOW

    assert ARCHIVE_NOW in TEXT


def test_the_machine_claims_match_the_heartbeat(tmp_path):
    profile = heartbeat_report(tmp_path)["system_profile"]

    assert "free space" in TEXT
    assert isinstance(profile["disk_free_bytes"], int)
    assert "disk_path" not in profile
    assert "available_ram_bytes" not in profile


def test_the_no_paths_claim_matches_what_the_wire_strips():
    assert "No file paths" in TEXT
    assert "path" in _DEVICE_LOCAL["videos"]


def test_the_preset_claims_match_the_contract():
    from deepreefmap_gui.sync import contract

    assert "assign this device a default preset" in TEXT
    # Presets flow down only: published by the console, never pushed from here.
    assert "presets" in contract.PULL_SECTIONS
    assert "presets" not in contract.PUSH_SECTIONS
