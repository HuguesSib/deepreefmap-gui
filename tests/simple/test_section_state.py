"""Per-step verdicts. Pure and Qt-free, so these need no window."""

from __future__ import annotations

import pytest

from deepreefmap_gui.simple.section_state import (
    ATTENTION,
    BLOCKED,
    CAUSE_RETIRED_TRANSECT,
    CAUSE_UNREAD_GRAVITY,
    FIX_HERE,
    FIX_MACHINE,
    FIX_SETTINGS,
    OK,
    TODO,
    SectionState,
    browse_state,
    headline,
    machine_state,
    run_gate,
    transects_state,
    videos_state,
)


def gate(**overrides):
    kwargs = {
        "pass_count": 1,
        "unassigned": 0,
        "remaining": 1,
        "failed": 0,
        "has_preset": True,
        "missing_models": [],
    }
    kwargs.update(overrides)
    return run_gate(**kwargs)


def test_plan_needs_one_saved_transect():
    assert transects_state(0, False).state == TODO
    assert transects_state(2, False).state == OK
    assert transects_state(2, False).count == "2 transects"
    assert transects_state(1, False).count == "1 transect"


def test_half_entered_transect_is_flagged():
    """Nothing else in the UI mentions a draft again, so the step has to."""
    state = transects_state(1, True)
    assert state.state == ATTENTION
    assert "both ends" in state.reason


def test_empty_run_step_is_todo_not_blocked():
    """No videos yet is the normal starting state, not a problem to solve."""
    state = gate(pass_count=0, remaining=0)
    assert state.state == TODO


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"has_preset": False}, "run settings"),
        ({"missing_models": ["coralscapes-vit-b-dpt"]}, "coralscapes-vit-b-dpt"),
        ({"gpu_only_mapper": "loger_star"}, "loger_star"),
    ],
)
def test_blockers_name_themselves(overrides, fragment):
    state = gate(pass_count=3, **overrides)
    assert state.state == BLOCKED
    assert fragment in state.reason


def test_a_skipped_transect_is_reported_but_never_blocks():
    """Scenario: a clip is queued with the transect deliberately skipped.

    Expected behaviour: the batch runs, and the step reports the pass as
    uncompared below every real blocker.
    """
    state = gate(pass_count=3, unassigned=1, remaining=3)
    assert state.state == OK
    assert "without a transect" in state.count
    assert "not compared" in state.reason


def test_an_unscaled_transect_is_reported_but_never_blocks():
    """Scenario: a pass sits on a transect whose tape length was never entered.

    Expected behaviour: the batch runs, and the step says the outputs will be
    unscaled while there is still time to enter the length. Below unassigned,
    because a missing transect swallows a missing tape reading.
    """
    state = gate(pass_count=2, remaining=2, unscaled=1)
    assert state.state == OK
    assert "unscaled" in state.count
    assert "tape length" in state.reason

    both = gate(pass_count=2, remaining=2, unassigned=1, unscaled=1)
    assert "without a transect" in both.count


def test_a_retired_transect_is_reported_and_never_blocks():
    """Scenario: the registry retired the line a queued pass was swum on, and it
    recorded a tape length before it went.

    Expected behaviour: the session still runs, scaled by that length, and the
    step says so. Blocking would strand footage already collected, and the diver
    cannot un-retire a line somebody else withdrew. Above the other warnings
    because nothing else on screen would mention it.
    """
    state = gate(pass_count=2, remaining=2, retired=1, failed=1, unassigned=1, unscaled=1)
    assert state.state == ATTENTION
    assert "retired transect" in state.count
    assert "scaled by the length recorded for it" in state.reason
    assert "web console" in state.reason
    assert "un-retire" not in state.reason


def test_a_retired_transect_with_no_length_says_the_run_will_be_unscaled():
    """Scenario: the line was withdrawn and it never carried a tape length, so
    the length handed to the reconstruction is nothing at all.

    Expected behaviour: the verdict says the runs will be unscaled. Testing
    retired before unscaled had it promise a scale the code cannot deliver, and
    it is the retired branch that has to carry the fact, because it hides the
    unscaled one underneath it.
    """
    state = gate(pass_count=2, remaining=2, retired=2, retired_unscaled=2, unscaled=2)

    assert "will run unscaled" in state.reason
    assert "scaled by the length recorded" not in state.reason
    assert "Set the length under Transects" not in state.reason, "nothing lists the line"
    assert "cannot be given a length here" in state.reason
    assert "will run unscaled" in headline(state.reason), "the notification's own title"


def test_a_retired_transect_says_how_many_of_its_passes_lose_their_scale():
    """Two lines were withdrawn and only one of them recorded a length, so
    neither "they scale" nor "they do not" is true of the group."""
    state = gate(pass_count=3, remaining=3, retired=3, retired_unscaled=1, unscaled=1)

    assert "1 of them will run unscaled" in state.reason
    assert "cannot be given a length here" in state.reason


def test_a_group_spread_over_two_withdrawn_lines_says_two():
    """The count of passes says nothing about how many lines they are on, and
    two passes of one withdrawn line is the ordinary case. Saying "a transect"
    of a group swum on two of them named a line that does not exist."""
    one = gate(pass_count=2, remaining=2, retired=2, retired_lines=1)
    assert "2 passes are on a transect the registry no longer lists" in one.reason
    assert "the length recorded for it" in one.reason
    assert "Ask whoever removed it" in one.reason

    two = gate(pass_count=2, remaining=2, retired=2, retired_lines=2)
    assert "2 passes are on 2 transects the registry no longer lists" in two.reason
    assert "the length recorded for them" in two.reason
    assert "Ask whoever removed them" in two.reason

    withdrawn = gate(pass_count=2, remaining=2, retired=2, retired_unscaled=2, unscaled=2, retired_lines=2)
    assert "no tape length was recorded for them" in withdrawn.reason
    assert "whether they should come back with their tape lengths" in withdrawn.reason


def test_the_mixed_sentence_does_not_claim_they_share_a_line():
    """Some of the group lost its scale and some did not, which is only possible
    across more than one line, so the clause cannot say "the line they are on"."""
    state = gate(pass_count=3, remaining=3, retired=3, retired_unscaled=1, unscaled=1, retired_lines=2)

    assert "1 of them will run unscaled" in state.reason
    assert "no tape length was recorded for the line each was swum on" in state.reason


def test_an_unscaled_pass_is_named_even_when_a_retired_line_is_the_reason():
    """Scenario: one pass is on a withdrawn line that did record a tape length,
    and another is on a line still listed that never had one.

    Expected behaviour: both are named. Only one reason is shown and the retired
    one outranks, so an unscaled reconstruction went unmentioned on every screen
    a diver reads before pressing Start. Everything else this gate reports can be
    put right afterwards; an unscaled run cannot.
    """
    state = gate(pass_count=2, remaining=2, retired=1, retired_unscaled=0, unscaled=1)

    assert state.cause == CAUSE_RETIRED_TRANSECT, "still one reason, not two verdicts"
    assert "the registry no longer lists" in state.reason
    assert "1 pass is on a listed transect with no tape length" in state.reason
    assert "Set the length under Transects" in state.reason
    assert "1 unscaled" in state.count


def test_the_unscaled_group_is_counted_over_the_lines_it_is_spread_across():
    """The same defect the retired half carried: the count of passes says
    nothing about how many lines they were swum on, and "a listed transect" of
    a group spread over three names a line nothing here is on."""
    one = gate(pass_count=4, remaining=4, retired=1, unscaled=2, unscaled_lines=1)
    assert "2 passes are on a listed transect with no tape length" in one.reason

    several = gate(pass_count=4, remaining=4, retired=1, unscaled=2, unscaled_lines=2)
    assert "2 passes are on 2 listed transects with no tape length" in several.reason

    alone = gate(pass_count=4, remaining=4, unscaled=3, unscaled_lines=2)
    assert "3 passes are on 2 transects with no tape length" in alone.reason
    assert "listed" not in alone.reason, "nothing was withdrawn to tell them apart from"

    uncounted = gate(pass_count=4, remaining=4, unscaled=3)
    assert "3 passes are on a transect with no tape length" in uncounted.reason


@pytest.mark.parametrize(
    "overrides",
    [
        {"unscaled": 1, "unscaled_lines": 1},
        {"unscaled": 3, "unscaled_lines": 2},
        {"retired": 2, "retired_unscaled": 2, "unscaled": 2, "retired_lines": 2},
        {"retired": 1, "retired_unscaled": 0, "unscaled": 2, "unscaled_lines": 2},
        {"retired": 2, "retired_unscaled": 1, "unscaled": 3, "unscaled_lines": 2},
    ],
)
def test_a_run_that_will_come_out_unscaled_is_never_the_half_that_is_buried(overrides):
    """The notification splits a reason at its first full stop, headline from
    body, so a fact in the second sentence never reaches a title. Of everything
    this gate reports, an unscaled reconstruction is the only one nothing
    afterwards can put right, so it is the one that cannot land in the body."""
    state = gate(pass_count=5, remaining=5, **overrides)

    assert "will run unscaled" in headline(state.reason)


def test_a_retired_group_that_covers_every_unscaled_pass_says_it_once():
    """The passes on the withdrawn line are the unscaled ones, so a second
    clause about a listed line would be describing nobody."""
    state = gate(pass_count=2, remaining=2, retired=2, retired_unscaled=2, unscaled=2)

    assert "listed transect" not in state.reason
    assert "unscaled" not in state.count


def test_one_pass_reads_as_one_pass():
    """A count and the verbs that follow it have to agree, in every branch that
    puts them in one sentence."""
    alone = gate(pass_count=1, retired=1)
    assert "1 pass is on" in alone.reason
    assert "still runs scaled" in alone.reason

    unscaled = gate(pass_count=1, retired=1, retired_unscaled=1)
    assert "so it will run unscaled" in unscaled.reason
    assert "1 pass is on" in gate(pass_count=1, unscaled=1).reason
    assert "so it will run unscaled" in gate(pass_count=1, unscaled=1).reason

    several = gate(pass_count=2, retired=2, retired_unscaled=2)
    assert "2 passes are on" in several.reason
    assert "so they will run unscaled" in several.reason


def test_a_real_blocker_outranks_a_skipped_transect():
    """Only one reason is shown, so it must be the one that stops the batch."""
    state = gate(
        pass_count=3,
        unassigned=1,
        has_preset=False,
        missing_models=["x"],
        gpu_only_mapper="loger",
    )
    assert state.state == BLOCKED
    assert "run settings" in state.reason


def test_a_gpu_only_method_on_a_cpu_laptop_blocks():
    """Without this the batch enables Process and every pass fails in turn."""
    state = gate(pass_count=2, gpu_only_mapper="loger_star")
    assert state.state == BLOCKED
    assert "graphics card" in state.reason


def test_the_missing_card_outranks_the_missing_models():
    """Changing the method changes what to download, so it is asked about first."""
    state = gate(pass_count=2, gpu_only_mapper="loger", missing_models=["LoGeR"])
    assert "graphics card" in state.reason


@pytest.mark.parametrize(
    "overrides, destination",
    [
        ({"unassigned": 1}, FIX_HERE),
        ({"has_preset": False}, FIX_SETTINGS),
        ({"gpu_only_mapper": "loger"}, FIX_MACHINE),
        ({"missing_models": ["x"]}, FIX_MACHINE),
    ],
)
def test_each_blocker_says_where_it_is_fixed(overrides, destination):
    """The strip's button reads this, so a blocker with nowhere to go says so."""
    assert gate(pass_count=2, **overrides).fix == destination


def test_no_blocker_tells_the_user_to_change_modes():
    """Simple mode cannot follow directions into the advanced sidebar."""
    for overrides in (
        {"has_preset": False},
        {"gpu_only_mapper": "loger"},
        {"missing_models": ["coralscapes-vit-b-dpt"]},
    ):
        reason = gate(pass_count=2, **overrides).reason.lower()
        assert "advanced" not in reason
        assert "tab" not in reason


def test_an_unknown_fix_destination_is_rejected():
    with pytest.raises(ValueError):
        SectionState(BLOCKED, "2 passes", "nowhere to go", fix="somewhere")


def test_failed_passes_warn_without_blocking():
    state = gate(pass_count=4, failed=2, remaining=2)
    assert state.state == ATTENTION
    assert "2 failed" in state.count


def test_a_finished_batch_is_ok():
    state = gate(pass_count=2, remaining=0)
    assert state.state == OK
    assert state.count == "2 passes · all processed"
    assert state.reason == ""


def test_browse_never_blocks_and_chases_unfiled_runs():
    assert browse_state(0, 0).state == TODO
    assert browse_state(5, 0).state == OK
    unfiled = browse_state(5, 2)
    assert unfiled.state == ATTENTION
    assert "2 unfiled" in unfiled.count


def test_an_unmet_requirement_blocks_the_machine_and_counts_itself():
    one = machine_state(unmet=1)
    assert one.state == BLOCKED
    assert one.count == "1 requirement not met"
    assert machine_state(unmet=3).count == "3 requirements not met"


def test_a_memory_advisory_alone_warns_rather_than_blocking():
    """The batch still runs on a machine that may run low, so nothing is blocked."""
    state = machine_state(unmet=0, advisory="This session may exhaust memory on this machine.")
    assert state.state == ATTENTION
    assert "exhaust memory" in state.reason


def test_an_available_update_is_named_without_raising_the_state():
    """An update is a chore rather than a blocker, so it says so and nothing more."""
    state = machine_state(unmet=0, update_version="2.1.0")
    assert state.state == OK
    assert "2.1.0" in state.reason


def test_an_update_alongside_a_blocker_still_leaves_the_machine_blocked():
    state = machine_state(unmet=2, update_version="2.1.0")
    assert state.state == BLOCKED
    assert "2 requirements" in state.reason
    assert "2.1.0" in state.reason


def test_an_update_alongside_an_advisory_stays_at_attention():
    state = machine_state(unmet=0, advisory="Memory may run low.", update_version="2.1.0")
    assert state.state == ATTENTION
    assert "Memory may run low." in state.reason
    assert "2.1.0" in state.reason


def test_a_machine_with_nothing_to_report_is_ok():
    state = machine_state(unmet=0)
    assert state.state == OK
    assert state.count == "Ready"


def test_the_machine_never_sends_the_user_anywhere_else():
    """Setup is where its own blockers are fixed, so it names no destination."""
    for state in (machine_state(unmet=2), machine_state(unmet=0, advisory="low memory")):
        assert state.fix == FIX_HERE


def test_unknown_state_is_rejected():
    with pytest.raises(ValueError):
        SectionState("nearly", "1 transect")


def test_unknown_cause_is_rejected():
    with pytest.raises(ValueError):
        SectionState(ATTENTION, "1 clip", cause="videos.eaten_by_a_shark")


def _speaking_verdicts():
    """Every verdict any of the five functions can return with something to say."""
    return [
        transects_state(1, True),
        browse_state(19, 17),
        videos_state(10, 10),
        gate(missing_files=2),
        gate(has_preset=False),
        gate(gpu_only_mapper="loger"),
        gate(missing_models=["dinov3"]),
        gate(failed=3),
        gate(retired=2),
        gate(unassigned=3),
        gate(unscaled=3),
        machine_state(unmet=2),
        machine_state(unmet=0, advisory="low memory"),
    ]


def test_every_verdict_worth_raising_names_its_cause():
    """The notification centre fingerprints on the cause, so a verdict with
    advice and no cause would be a message it could never track or silence."""
    for verdict in _speaking_verdicts():
        assert verdict.cause, verdict.reason


def test_a_cause_survives_a_reworded_reason():
    """Rewording must not read as a different problem, or every reader's
    decision to never hear this one again is silently voided."""
    assert videos_state(10, 10).cause == videos_state(10, 4).cause


def test_a_count_verdict_carries_the_number_behind_its_words():
    assert browse_state(19, 17).n == 17
    assert videos_state(10, 4).n == 4
    assert gate(failed=3).n == 3
    assert gate(missing_models=["a", "b"]).n == 2


def test_badge_vocabulary_matches_the_verdicts():
    """core/icons.py spells the states out rather than importing upwards, so
    the two lists have to be checked against each other."""
    from deepreefmap_gui.core.icons import STEP_STATES
    from deepreefmap_gui.simple.section_state import SECTION_STATES

    assert set(STEP_STATES) == set(SECTION_STATES)


def test_a_headline_drops_the_advice_and_keeps_the_fault():
    """A one-line surface has room for the fault, not the advice after it."""
    reason = browse_state(19, 17).reason
    assert headline(reason) == "17 runs belong to no transect"
    assert "Assign them" in reason


def test_a_one_sentence_reason_survives_whole():
    assert headline("Add a transect, or import a CSV or GPX file.") == ("Add a transect, or import a CSV or GPX file")


def test_gravity_this_platform_cannot_read_is_said_without_blocking():
    state = gate(pass_count=3, unread_gravity=2)
    assert state.state == OK
    assert "2 without gravity" in state.count
    assert "not gravity-aligned" in state.reason
    assert state.cause == CAUSE_UNREAD_GRAVITY


def test_unread_gravity_yields_to_every_other_reason():
    state = gate(pass_count=3, unread_gravity=2, unscaled=1)
    assert state.cause != CAUSE_UNREAD_GRAVITY
