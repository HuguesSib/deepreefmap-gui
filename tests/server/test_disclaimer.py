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


def sentence(marker: str) -> str:
    """The one sentence carrying a claim, so a test can pin that claim alone.

    A whole-text search would pass on a promise made in the wrong sentence,
    which is the failure these two-sided tests exist to catch: what comes down
    and what never does are the same words in opposite places.
    """
    for said in TEXT.split(". "):
        if marker in said:
            return said
    raise AssertionError(f"No sentence of the disclaimer says {marker!r}")


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


def test_the_downward_claim_names_every_kind_the_registry_can_write_here():
    """Exhaustive by design: a new pull section has to be added to the sentence."""
    from deepreefmap_gui.sync import contract

    said = sentence("replaces or removes the copy here").lower()

    for section in contract.PULL_SECTIONS:
        assert SECTION_LABELS[section].lower() in said


def test_the_safe_claim_covers_only_what_the_registry_never_writes_here():
    from deepreefmap_gui.sync import contract

    said = sentence("only ever sent").lower()
    only_ours = [name for name in AUTHORED_SECTIONS if name not in contract.PULL_SECTIONS]

    assert only_ours
    for section in only_ours:
        assert SECTION_LABELS[section].lower() in said
    for section in contract.PULL_SECTIONS:
        assert SECTION_LABELS[section].lower() not in said


def test_a_deletion_in_the_console_travels_as_a_column_on_what_comes_down():
    """A deletion is a tombstone landing like any other row, not a second verb."""
    from deepreefmap_gui.sync import contract

    assert "edited or deleted in the web console" in TEXT
    for section in contract.PULL_SECTIONS:
        columns = {
            column["name"]
            for table in contract.DOCUMENT["tables"]
            if table["section"] == section
            for column in table["columns"]
        }
        assert "deleted_at" in columns


def test_the_promise_that_an_overwrite_is_reported_is_one_a_pull_keeps():
    """The sentence promises a report, so a pull that overwrote has to post one."""
    import uuid
    from pathlib import Path

    from deepreefmap_gui.sync.engine import CONFLICT_OVERWRITTEN, PullReport, SyncEngine

    posted: list[str] = []

    class Sink:
        def post(self, *, fingerprint, title, body="", severity="", scope=""):
            posted.append(fingerprint)

    assert "the app says so when that overwrites an edit" in TEXT
    # Neither the store nor the client is read while conflicts are reported.
    engine = SyncEngine(None, None, out_root=Path("."), notifications=Sink())
    engine._report_pull_conflicts(PullReport(overwritten=(uuid.uuid4(),)))

    assert CONFLICT_OVERWRITTEN in posted
