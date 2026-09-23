"""Turn-card grouping for the job-detail stream (poly-qqx9.4).

Replaces the attempt-accordion grouping these rows used to feed. The unit
is the turn, so the interesting cases are all joins: a tool row finding
its turn, a tool_start finding its completion, and a verdict that does
not invent a failure the job never had.
"""

from __future__ import annotations

from dportsv3.tracker.render import group_activity_into_cards


def _row(id, stage, extra=None, message=None, duration_ms=None, ts=None):
    return {
        "id": id, "stage": stage, "extra": extra,
        "message": message if message is not None else stage,
        "duration_ms": duration_ms,
        "ts": ts or "2026-09-23T10:00:00+00:00",
    }


def _patch_job():
    """One attempt: two turns, a passing tool, a failing tool, an end."""
    return [
        _row(1, "decision", {"action": "patch", "tier": "cheap"}),
        _row(2, "attempt_start", {"attempt": 1, "iterations": 3,
                                  "budget": 400000, "tokens_used_so_far": 0},
             ts="2026-09-23T10:00:00+00:00"),
        _row(3, "llm_turn", {"attempt": 1, "turn": 1, "total_tokens": 900,
                             "billable_tokens": 700, "text": "Reading the log.",
                             "tools_requested": ["get_file"]}),
        _row(4, "tool_start", {"attempt": 1, "turn": 1, "tool": "get_file",
                               "call_id": "c1", "args": {"path": "Makefile"}}),
        _row(5, "tool:get_file", {"attempt": 1, "turn": 1, "ok": True,
                                  "call_id": "c1", "args": {"path": "Makefile"}},
             message="read 40 lines", duration_ms=118),
        _row(6, "llm_turn", {"attempt": 1, "turn": 2, "total_tokens": 1200,
                             "billable_tokens": 800, "text": "Patching it.",
                             "tools_requested": ["dsynth_build"]}),
        _row(7, "tool_start", {"attempt": 1, "turn": 2, "tool": "dsynth_build",
                               "call_id": "c2", "args": {"origin": "devel/foo"}}),
        _row(8, "tool:dsynth_build", {"attempt": 1, "turn": 2, "ok": False,
                                      "call_id": "c2", "rc": 1,
                                      "stderr_tail": "ld: undefined symbol"},
             message="build failed", duration_ms=2_640_000),
        _row(9, "attempt_end", {"attempt": 1, "rebuild_ok": False},
             ts="2026-09-23T10:44:00+00:00"),
    ]


def _kinds(cards):
    return [c["kind"] for c in cards]


def test_cards_are_newest_first_so_the_running_turn_is_on_top():
    cards = group_activity_into_cards(_patch_job())
    assert [c["id"] for c in cards] == [9, 6, 3, 2, 1]
    assert _kinds(cards) == ["boundary", "turn", "turn", "boundary", "stage"]


def test_input_order_does_not_matter():
    """The detail route passes rows newest-first; grouping is by id."""
    rows = _patch_job()
    shuffled = list(reversed(rows[:4])) + rows[4:]
    assert group_activity_into_cards(shuffled) == group_activity_into_cards(rows)


def test_a_tool_row_lands_inside_the_turn_that_called_it():
    cards = group_activity_into_cards(_patch_job())
    t1 = next(c for c in cards if c["kind"] == "turn" and c["turn"] == 1)
    assert [t["name"] for t in t1["tools"]] == ["get_file"]
    assert t1["text"] == "Reading the log."
    assert t1["total_tokens"] == 900


def test_tool_start_and_completion_fold_into_one_entry_by_call_id():
    """The pair is one tool, not two rows: poly-qqx9.2 writes both."""
    cards = group_activity_into_cards(_patch_job())
    t1 = next(c for c in cards if c["kind"] == "turn" and c["turn"] == 1)
    assert len(t1["tools"]) == 1
    tool = t1["tools"][0]
    assert tool["running"] is False
    assert tool["duration_ms"] == 118
    assert tool["args"] == {"path": "Makefile"}
    assert tool["summary"] == "read 40 lines"


def test_a_start_without_its_completion_is_the_running_state():
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 2, "turn": 5, "total_tokens": 10}),
        _row(2, "tool_start", {"attempt": 2, "turn": 5, "tool": "dsynth_test",
                               "call_id": "c9", "args": {}}),
    ])
    turn = cards[0]
    assert turn["state"] == "run"
    assert turn["tools"][0]["running"] is True
    assert turn["tools"][0]["state"] == "run"


def test_rows_predating_call_id_pair_by_name():
    """Jobs written before poly-qqx9.2 have no tool_start at all; ones
    written between it and the call_id landing have no id to pair on."""
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1}),
        _row(2, "tool_start", {"attempt": 1, "turn": 1, "tool": "grep"}),
        _row(3, "tool:grep", {"attempt": 1, "turn": 1, "ok": True},
             duration_ms=9),
    ])
    assert len(cards[0]["tools"]) == 1
    assert cards[0]["tools"][0]["duration_ms"] == 9


def test_a_completion_with_no_start_still_renders_as_a_tool():
    """Every job that ran before poly-qqx9.2 looks like this."""
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1}),
        _row(2, "tool:get_file", {"attempt": 1, "turn": 1, "ok": True},
             duration_ms=12),
    ])
    assert [t["name"] for t in cards[0]["tools"]] == ["get_file"]
    assert cards[0]["state"] == "ok"


def test_a_failed_tool_makes_the_turn_read_failed():
    cards = group_activity_into_cards(_patch_job())
    t2 = next(c for c in cards if c["kind"] == "turn" and c["turn"] == 2)
    assert t2["state"] == "bad"
    assert t2["tools"][0]["rc"] == 1
    assert t2["tools"][0]["stderr_tail"] == "ld: undefined symbol"


def test_ok_none_is_no_verdict_not_a_failure():
    """steps.py:238 writes None when the result is not a dict. Colouring
    that red invents a failure the job never had."""
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1}),
        _row(2, "tool:odd", {"attempt": 1, "turn": 1, "ok": None}),
    ])
    assert cards[0]["tools"][0]["state"] == "info"
    assert cards[0]["state"] == "info"


def test_a_running_tool_outranks_an_earlier_pass_in_the_same_turn():
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1}),
        _row(2, "tool:get_file", {"attempt": 1, "turn": 1, "ok": True}),
        _row(3, "tool_start", {"attempt": 1, "turn": 1, "tool": "dsynth_build",
                               "call_id": "c1"}),
    ])
    assert cards[0]["state"] == "run"


def test_a_failure_outranks_everything_in_the_same_turn():
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 1}),
        _row(2, "tool:get_file", {"attempt": 1, "turn": 1, "ok": True}),
        _row(3, "tool:write_file", {"attempt": 1, "turn": 1, "ok": False}),
        _row(4, "tool_start", {"attempt": 1, "turn": 1, "tool": "grep",
                               "call_id": "c2"}),
    ])
    assert cards[0]["state"] == "bad"


def test_a_text_only_turn_is_the_final_answer_not_an_unknown():
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"attempt": 1, "turn": 3, "text_only": True,
                             "text": "The patch is complete."}),
    ])
    assert cards[0]["state"] == "ok"
    assert cards[0]["tools"] == []


def test_the_attempt_end_card_carries_what_the_accordion_header_did():
    cards = group_activity_into_cards(_patch_job())
    end = next(c for c in cards if c.get("edge") == "end")
    assert end["rebuild_ok"] is False
    assert end["state"] == "bad"
    assert end["n_turns"] == 2
    assert end["n_tools"] == 2
    # BILLABLE, never the provider total: summing the re-billed total made
    # the same attempt read 19x apart on one screen (poly-0g0).
    assert end["tokens"] == 1500
    assert end["elapsed_s"] == 44 * 60


def test_an_attempt_start_card_carries_the_budget_it_opened_with():
    cards = group_activity_into_cards(_patch_job())
    start = next(c for c in cards if c.get("edge") == "start")
    assert start["state"] == "bound"
    assert (start["budget"], start["iterations"]) == (400000, 3)


def test_an_attempt_with_no_end_yields_no_end_card():
    cards = group_activity_into_cards([
        _row(1, "attempt_start", {"attempt": 1}),
        _row(2, "llm_turn", {"attempt": 1, "turn": 1}),
    ])
    assert [c.get("edge") for c in cards if c["kind"] == "boundary"] == ["start"]


def test_unmeasurable_elapsed_is_none_not_zero():
    cards = group_activity_into_cards([
        _row(1, "attempt_start", {"attempt": 1}, ts="not-a-timestamp"),
        _row(2, "attempt_end", {"attempt": 1, "rebuild_ok": True}),
    ])
    end = next(c for c in cards if c.get("edge") == "end")
    assert end["elapsed_s"] is None


def test_billable_falls_back_to_total_for_rows_that_predate_the_field():
    cards = group_activity_into_cards([
        _row(1, "attempt_start", {"attempt": 1}),
        _row(2, "llm_turn", {"attempt": 1, "turn": 1, "total_tokens": 321}),
        _row(3, "attempt_end", {"attempt": 1, "rebuild_ok": True}),
    ])
    assert next(c for c in cards if c.get("edge") == "end")["tokens"] == 321


def test_triage_turns_have_no_attempt_and_no_boundary():
    """triage.py emits phase="triage" with a turn but no attempt."""
    cards = group_activity_into_cards([
        _row(1, "llm_turn", {"phase": "triage", "turn": 1,
                             "total_tokens": 50, "text_only": True}),
        _row(2, "llm_turn", {"phase": "triage", "turn": 2,
                             "total_tokens": 60, "text_only": True}),
    ])
    assert _kinds(cards) == ["turn", "turn"]
    assert [c["attempt"] for c in cards] == [None, None]
    assert [c["turn"] for c in cards] == [2, 1]


def test_a_job_with_no_turns_at_all_is_a_stream_of_stage_cards():
    """verify and confirm have no on_event: their activity is flat
    operational stages. An empty "Turns" heading is the failure mode this
    avoids."""
    cards = group_activity_into_cards([
        _row(1, "verify_branch_checkout"),
        _row(2, "verify_failed", {"diag_tail": "patch does not apply"}),
    ])
    assert _kinds(cards) == ["stage", "stage"]
    failed = cards[0]
    assert failed["stage"] == "verify_failed"
    assert failed["state"] == "bad"
    assert failed["diag_tail"] == "patch does not apply"


def test_a_stage_row_that_did_not_fail_reads_neutral():
    cards = group_activity_into_cards([_row(1, "verify_complete")])
    assert cards[0]["state"] == "info"


def test_a_tool_row_whose_turn_fell_outside_the_window_still_groups():
    """The route fetches the newest N rows, so the oldest card can be
    missing its llm_turn. Synthesize the card rather than dropping the
    calls or printing 0 tokens as though they had been measured."""
    cards = group_activity_into_cards([
        _row(2, "tool:get_file", {"attempt": 1, "turn": 4, "ok": True}),
        _row(3, "tool:grep", {"attempt": 1, "turn": 4, "ok": True}),
    ])
    assert len(cards) == 1
    assert cards[0]["partial"] is True
    assert cards[0]["turn"] == 4
    assert len(cards[0]["tools"]) == 2


def test_a_row_with_no_turn_at_all_becomes_a_stage_card():
    cards = group_activity_into_cards([
        _row(1, "tool:get_file", {"ok": True}),
    ])
    assert _kinds(cards) == ["stage"]


def test_empty_activity_returns_no_cards():
    assert group_activity_into_cards([]) == []
