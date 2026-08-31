"""Reading COLMAP's console output.

Scenario: COLMAP is the only thing that knows how far into a stage it is, and it
says so on file descriptor 2 in glog's format. The lines below are verbatim from
a run of the calibrator, including one that failed to reconstruct.
"""

from __future__ import annotations

import os

from deepreefmap_gui.camera.colmap_output import (
    DISCARDED,
    EXTRACTED,
    KEPT,
    MATCHED,
    REGISTERED,
    WARNING,
    capture_stderr,
    parse_line,
)

EXTRACTING = "I20260831 16:10:25.148383 140557695309504 feature_extraction.cc:270] Processed file [1/100]"
MATCHING = "I20260831 16:10:49.764287 140558693553856 pairing.cc:485] Processing image [91/100]"
REGISTERING = (
    "I20260831 16:11:11.366572 140560648103616 incremental_pipeline.cc:537] "
    "Registering image #36 (num_reg_frames=2)"
)
NO_PAIR = (
    "I20260831 16:11:11.375282 140560648103616 incremental_pipeline.cc:697] "
    "Discarding reconstruction due to no initial pair"
)
TOO_SMALL = (
    "W20260831 16:11:11.366975 140560648103616 incremental_pipeline.cc:718] "
    "Discarding reconstruction due to insufficient size"
)
SUCCESS = (
    "I20260831 16:09:08.409524 140560648103616 incremental_pipeline.cc:723] Keeping successful reconstruction"
)
THREAD_WARNING = (
    "W20260831 16:10:22.071441 140560648103616 feature_extraction.cc:445] Your current options use the "
    "maximum number of threads on the machine to extract features."
)
CHATTER = "I20260831 16:09:02.853179 140558402021056 sift.cc:763] Creating SIFT CPU feature extractor"


def test_extraction_counts_the_frames_processed():
    event = parse_line(EXTRACTING)

    assert (event.kind, event.done, event.total) == (EXTRACTED, 1, 100)


def test_matching_counts_the_images_paired():
    event = parse_line(MATCHING)

    assert (event.kind, event.done, event.total) == (MATCHED, 91, 100)


def test_registering_reports_the_frames_placed_so_far():
    event = parse_line(REGISTERING)

    assert (event.kind, event.done) == (REGISTERED, 2)


def test_a_discarded_reconstruction_carries_its_reason():
    assert parse_line(NO_PAIR) == parse_line(NO_PAIR)
    assert parse_line(NO_PAIR).kind == DISCARDED
    assert parse_line(NO_PAIR).text == "no initial pair"
    assert parse_line(TOO_SMALL).text == "insufficient size"


def test_a_kept_reconstruction_is_reported():
    assert parse_line(SUCCESS).kind == KEPT


def test_a_warning_travels_as_one():
    event = parse_line(THREAD_WARNING)

    assert event.kind == WARNING
    assert "maximum number of threads" in event.text


def test_the_rest_of_the_chatter_is_not_an_event():
    assert parse_line(CHATTER) is None
    assert parse_line("") is None
    assert parse_line("not a glog line at all") is None


def test_writing_to_the_descriptor_reaches_the_reader():
    """The C++ writes to fd 2 directly, so the descriptor is what must be taken
    over: a `sys.stderr` replacement never sees them."""
    seen = []

    with capture_stderr(seen.append):
        os.write(2, b"first line\nsecond line\n")

    assert seen == ["first line", "second line"]


def test_the_real_stderr_comes_back_afterwards():
    before = os.fstat(2)

    with capture_stderr(lambda _: None):
        pass

    assert os.fstat(2) == before
    os.write(2, b"")
