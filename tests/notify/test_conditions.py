"""Pass verdicts becoming messages. Driven through the production verdicts."""

from __future__ import annotations

from pathlib import Path

from deepreefmap_gui.notify.conditions import (
    DB_UNREADABLE,
    condition_from_verdict,
    conditions_from_state,
)
from deepreefmap_gui.simple.section_state import (
    CAUSE_MISSING_CLIPS,
    CAUSE_UNASSIGNED_PASSES,
    browse_state,
    machine_state,
    run_gate,
    transects_state,
    videos_state,
)
from deepreefmap_gui.survey.health import SurveyDbHealth, SurveyDbState
from deepreefmap_gui.survey.models.notification import BLOCKER, INFO, MACHINE, SURVEY, WARNING


def gate(**overrides):
    kwargs = {
        "pass_count": 4,
        "unassigned": 0,
        "remaining": 4,
        "failed": 0,
        "has_preset": True,
        "missing_models": [],
    }
    kwargs.update(overrides)
    return run_gate(**kwargs)


def test_a_settled_destination_says_nothing():
    for verdict in (transects_state(2, False), browse_state(19, 0), videos_state(10, 0), gate()):
        assert condition_from_verdict("videos", verdict) is None


def test_a_warning_carries_the_fault_and_keeps_the_advice():
    condition = condition_from_verdict("videos", videos_state(10, 10))

    assert condition.title == "10 clips cannot be found"
    assert "Plug the drive back in" in condition.body
    assert condition.severity == WARNING
    assert condition.subject_count == 10
    assert condition.section == "videos"


def test_a_blocker_is_raised_as_one():
    condition = condition_from_verdict("process", gate(has_preset=False))

    assert condition.severity == BLOCKER


def test_advice_that_blocks_nothing_is_still_worth_saying_once():
    """A pass with no transect runs perfectly well, but cannot be compared
    afterwards, and there is time to fix that before it starts."""
    condition = condition_from_verdict("process", gate(unassigned=3))

    assert condition.severity == INFO
    assert condition.fingerprint == CAUSE_UNASSIGNED_PASSES


def test_the_fingerprint_is_the_cause_and_not_the_words():
    ten = condition_from_verdict("videos", videos_state(10, 10))
    four = condition_from_verdict("videos", videos_state(10, 4))

    assert ten.fingerprint == four.fingerprint == CAUSE_MISSING_CLIPS
    assert ten.title != four.title


def test_the_whole_header_is_folded_into_one_list():
    conditions = conditions_from_state(
        {
            "transects": transects_state(2, False),
            "videos": videos_state(10, 10),
            "process": gate(failed=2),
            "browse": browse_state(19, 17),
        }
    )

    assert len(conditions) == 3
    assert all(c.scope == SURVEY for c in conditions)


def test_the_computer_speaks_for_itself():
    conditions = conditions_from_state({}, machine_state(unmet=2))

    assert [c.scope for c in conditions] == [MACHINE]
    assert conditions[0].section == "machine"


def test_a_healthy_computer_says_nothing():
    assert conditions_from_state({}, machine_state(unmet=0)) == []


def test_a_survey_that_will_not_open_blocks_and_points_at_setup():
    health = SurveyDbHealth(
        SurveyDbState.CORRUPT, Path("/tmp/survey.db"), 8, detail="The file is not a database."
    )
    (condition,) = conditions_from_state({}, None, health)

    assert condition.fingerprint == DB_UNREADABLE
    assert condition.severity == BLOCKER
    assert condition.section == "machine"


def test_an_openable_survey_raises_nothing():
    health = SurveyDbHealth(SurveyDbState.MISSING, Path("/tmp/survey.db"), 8)

    assert conditions_from_state({}, None, health) == []
