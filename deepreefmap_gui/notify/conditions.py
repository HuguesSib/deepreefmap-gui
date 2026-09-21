"""The one place a pass verdict becomes a message.

Verdicts are matched on ``SectionState.cause``, never on the sentence they
carry. The sentence is prose and will be improved; the cause is the identity a
reader's decision to silence something is filed under.
"""

from __future__ import annotations

from collections.abc import Mapping

from deepreefmap_gui.notify.model import Condition
from deepreefmap_gui.simple.section_state import (
    ATTENTION,
    BLOCKED,
    CAUSE_NONE,
    SectionState,
    advice,
    headline,
)
from deepreefmap_gui.survey.health import SurveyDbHealth
from deepreefmap_gui.survey.models.notification import (
    BLOCKER,
    INFO,
    MACHINE,
    SURVEY,
    WARNING,
)

_SEVERITY = {BLOCKED: BLOCKER, ATTENTION: WARNING}

# A survey nobody can open is the one condition with nowhere to be written down,
# so it is stated here rather than derived from a verdict.
DB_UNREADABLE = "survey.db_unreadable"


def condition_from_verdict(
    section: str, verdict: SectionState, *, scope: str = SURVEY
) -> Condition | None:
    """A verdict's message, or None when it has nothing to raise.

    An OK verdict with a cause is deliberate: a pass that will run without a
    transect is not a problem, and blocks nothing, but it is worth saying once
    while there is still time to assign it.
    """
    if verdict.cause == CAUSE_NONE:
        return None
    return Condition(
        fingerprint=verdict.cause,
        severity=_SEVERITY.get(verdict.state, INFO),
        scope=scope,
        title=headline(verdict.reason),
        # The advice only. headline and advice split the reason in two, so a row
        # showing both says it once.
        body=advice(verdict.reason),
        section=section,
        subject_count=verdict.n,
    )


def conditions_from_state(
    sections: Mapping[str, SectionState],
    machine: SectionState | None = None,
    db_health: SurveyDbHealth | None = None,
) -> list[Condition]:
    """Everything true right now, from the verdicts the header already computes."""
    live = [
        condition
        for name, verdict in sections.items()
        if (condition := condition_from_verdict(name, verdict)) is not None
    ]
    if machine is not None:
        found = condition_from_verdict("machine", machine, scope=MACHINE)
        if found is not None:
            live.append(found)
    if db_health is not None and not db_health.openable:
        live.append(
            Condition(
                fingerprint=DB_UNREADABLE,
                severity=BLOCKER,
                scope=SURVEY,
                title="This survey cannot be opened",
                body=db_health.detail or f"The survey database is {db_health.state.value}.",
                section="machine",
            )
        )
    return live
