"""The now-bar: what the agent is doing, pinned (poly-qqx9.3).

Two rules carry most of these cases. The denominators are STORED, not
settings — attempt_start carries the iterations and budget the run
actually used. And absent is not zero: a value the job never recorded
renders as nothing, which is poly-0e02's rule from the farm-telemetry
correction.
"""

from __future__ import annotations

from pathlib import Path

from dportsv3.tracker.render import group_activity_into_cards, now_bar


def _row(id, stage, extra=None, ts=None, duration_ms=None):
    return {
        "id": id, "stage": stage, "extra": extra, "message": stage,
        "duration_ms": duration_ms,
        "ts": ts or "2026-09-23T10:00:00+00:00",
    }


def _running_job():
    return group_activity_into_cards([
        _row(1, "attempt_start", {"attempt": 3, "iterations": 4,
                                  "budget": 120000,
                                  "tokens_used_so_far": 61000}),
        _row(2, "llm_turn", {"attempt": 3, "turn": 7, "total_tokens": 52180,
                             "billable_tokens": 9000,
                             "cumulative_billable_tokens": 70000}),
        _row(3, "tool_start", {"attempt": 3, "turn": 7, "tool": "dsynth_test",
                               "call_id": "c1",
                               "args": {"origin": "devel/llvm19"}},
             ts="2026-09-23T10:05:00+00:00"),
    ])


ATTEMPT = {"attempt": 3, "iterations": 4, "budget": 120000,
           "tokens_used_so_far": 61000}


def test_the_bar_says_where_the_job_is_and_what_it_is_doing():
    bar = now_bar(_running_job(), ATTEMPT, {"tier": "ASSIST"})
    assert bar["attempt"] == 3
    assert bar["attempts_total"] == 4
    assert bar["turn"] == 7
    assert bar["tier"] == "ASSIST"
    assert bar["tool"]["name"] == "dsynth_test"
    assert bar["tool"]["args"] == {"origin": "devel/llvm19"}
    assert bar["tool"]["since"] == "2026-09-23T10:05:00+00:00"


def test_spend_adds_what_the_attempt_opened_with_to_what_it_has_spent():
    """budget is the JOB's (attempt_loop enforces it across attempts);
    a turn's cumulative_billable_tokens is THIS ATTEMPT's (tool_loop's
    accumulator is created per call). Showing the second against the
    first understates by every earlier attempt."""
    bar = now_bar(_running_job(), ATTEMPT, {})
    assert bar["spend"] == 61000 + 70000
    assert bar["budget"] == 120000


def test_spend_falls_back_to_the_opening_figure_when_a_turn_has_none():
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 2, "turn": 1, "total_tokens": 900}),
    ])
    assert now_bar(cards, {"tokens_used_so_far": 44000}, {})["spend"] == 44000


def test_spend_is_this_attempt_alone_on_the_first_attempt():
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1,
                             "cumulative_billable_tokens": 5000}),
    ])
    assert now_bar(cards, {"tokens_used_so_far": 0}, {})["spend"] == 5000


def test_the_denominator_comes_off_the_stored_row_not_a_setting():
    """runner.max_patch_attempts at render time would misreport any job
    that ran under a different setting."""
    bar = now_bar(_running_job(), {"attempt": 3, "iterations": 9}, {})
    assert bar["attempts_total"] == 9


def test_no_tool_running_renders_absent_not_zero():
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 2}),
        _row(2, "tool:grep", {"attempt": 1, "turn": 2, "ok": True}),
    ])
    bar = now_bar(cards, {}, {})
    assert bar["tool"] is None


def test_an_unrecorded_budget_is_absent_not_zero():
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1}),
    ])
    bar = now_bar(cards, {}, {})
    assert bar["budget"] is None
    assert bar["attempts_total"] is None


def test_a_turn_with_no_cumulative_billable_has_no_spend_to_show():
    """Rows written before the field existed have no honest substitute."""
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1, "total_tokens": 900}),
    ])
    assert now_bar(cards, {}, {})["spend"] is None


def test_the_attempt_falls_back_to_the_stored_row_when_turns_lack_it():
    """Triage turns carry no attempt at all."""
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"phase": "triage", "turn": 2}),
    ])
    bar = now_bar(cards, {"attempt": 1, "iterations": 3}, {})
    assert bar["attempt"] == 1


def test_a_manual_job_gets_no_bar_rather_than_a_zero_one():
    """MANUAL never runs the agent."""
    assert now_bar(_running_job(), ATTEMPT, {"tier": "MANUAL"}) is None


def test_a_job_with_no_turns_gets_no_bar():
    """verify and confirm have no loop: there is nothing to pin."""
    cards = group_activity_into_cards([
        _row(1, "verify_branch_checkout"),
        _row(2, "verify_complete"),
    ])
    assert now_bar(cards, {}, {}) is None


def test_only_the_newest_turn_can_hold_a_running_tool():
    """A call in an older turn is finished by definition -- the loop
    dispatches serially. Looking further back would report the tool a
    dead runner abandoned as running forever."""
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1}),
        _row(2, "tool_start", {"attempt": 1, "turn": 1, "tool": "stuck",
                               "call_id": "old"}),
        _row(3, "llm_turn", {"attempt": 1, "turn": 2}),
        _row(4, "tool:grep", {"attempt": 1, "turn": 2, "ok": True}),
    ])
    assert now_bar(cards, {}, {})["tool"] is None


def test_the_bar_renders_absent_cells_as_nothing():
    """The template must not print a 0 where the job recorded nothing."""
    import jinja2  # noqa: PLC0415
    import dportsv3.tracker as tracker  # noqa: PLC0415

    templates = Path(tracker.__file__).parent / "templates"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(templates)), autoescape=True)
    html = env.get_template("_now_bar.html").render(now={
        "attempt": 2, "attempts_total": None, "turn": 5, "tool": None,
        "spend": None, "budget": None, "tier": None,
    })
    assert "attempt 2" in html
    assert "/0" not in html
    assert "0 /" not in html
    assert "no tool running" in html
