"""poly-5e1: a retry is a continuation, not a restart.

``reset_attempt_workspace`` clears the WRKDIR and genpatch-out but never
touches ``ports/<origin>/``, so the previous attempt's overlay survives.
Measured, ``emit_diff`` by attempt on one four-attempt job: 0b, 1694b,
2116b, 2204b. The model could not tell, so it re-derived: 52% of a
retry's file reads repeated a read an earlier attempt had already made.

These tests pin what the retry's opening message must carry, and the two
ways it must not lie -- never asserting "nothing changed" over the top of
its own Changed list, and never presenting the carried diff as something
to defend (the poly-9u2 failure mode, one code path over).
"""
from __future__ import annotations

from unittest.mock import patch

from dportsv3.agent import attempt_loop as al


_LOG = [
    {"tool": "get_file", "args": {"path": "ports/devel/glib20/overlay.dops"},
     "result": {"ok": True}},
    {"tool": "list_dir", "args": {"path": "ports/devel/glib20"},
     "result": {"ok": True}},
    # a repeat -- the message must not list it twice
    {"tool": "get_file", "args": {"path": "ports/devel/glib20/overlay.dops"},
     "result": {"ok": True}},
    {"tool": "put_file", "args": {"path": "ports/devel/glib20/dragonfly/patch-gio"},
     "result": {"ok": True}},
    {"tool": "dsynth_build", "args": {"origin": "devel/glib20"},
     "result": {"ok": False, "rebuild_ok": False,
                "stderr_tail": "meson.build:214: ERROR: unsupported platform"}},
]

_DIFF = "--- a/overlay.dops\n+++ b/overlay.dops\n+USES=meson\n"


def _msg(diff="", log=None, **kw):
    with patch.object(al, "_current_diff", return_value=diff):
        return al._failure_context_message(
            1, kw.pop("prev_text", ""), env="e", origin="devel/glib20",
            tool_log=_LOG if log is None else log, **kw
        )["content"]


# --- what the previous attempt established reaches the retry -----------------


def test_the_files_it_read_are_carried():
    out = _msg(diff=_DIFF)
    assert "ports/devel/glib20/overlay.dops" in out
    assert "ports/devel/glib20" in out


def test_a_repeated_read_is_listed_once():
    """The point is to stop re-reading; listing a file twice teaches
    nothing and pays for the noise."""
    out = _msg(diff=_DIFF)
    assert out.count("- ports/devel/glib20/overlay.dops\n") == 1


def test_what_it_changed_is_carried():
    assert "ports/devel/glib20/dragonfly/patch-gio" in _msg(diff=_DIFF)


def test_the_build_error_is_carried():
    assert "meson.build:214: ERROR: unsupported platform" in _msg(diff=_DIFF)


def test_a_build_that_succeeded_is_not_reported_as_the_failure():
    log = [{"tool": "dsynth_build", "args": {"origin": "o"},
            "result": {"ok": True, "rebuild_ok": True, "stderr_tail": "noise"}}]
    assert "noise" not in _msg(diff=_DIFF, log=log)


def test_the_surviving_diff_is_carried_verbatim():
    out = _msg(diff=_DIFF)
    assert "+USES=meson" in out
    assert "```diff" in out


def test_the_stop_reason_is_stated_in_words_the_model_can_act_on():
    """A bare "turn_cap" tells the model nothing; "ran out of tool
    turns" tells it to be more direct this time."""
    out = _msg(diff=_DIFF, stop_reason="turn_cap")
    assert "ran out of tool turns" in out
    assert "turn_cap" not in out


def test_an_unknown_stop_reason_is_passed_through_not_dropped():
    assert "something_new" in _msg(diff=_DIFF, stop_reason="something_new")


def test_the_reset_boundary_is_spelled_out():
    """The model re-runs make_extract because it cannot tell what was
    wiped. Saying so is the whole saving."""
    out = _msg(diff=_DIFF)
    assert "genpatch-out" in out
    assert "make_extract" in out
    assert "not** reset" in out


# --- the two ways it must not lie --------------------------------------------


def test_it_never_claims_nothing_changed_while_listing_changes():
    """An unreadable diff must not become "the attempt left no changes":
    the Changed list two paragraphs up says otherwise."""
    out = _msg(diff=None)
    assert "no changes on disk" not in out
    assert "could not be read" in out


def test_an_empty_diff_with_no_writes_does_say_nothing_survived():
    log = [_LOG[0]]  # a read, no writes
    out = _msg(diff="", log=log)
    assert "no changes on disk" in out


def test_an_empty_diff_with_writes_does_not_assert_emptiness():
    out = _msg(diff="", log=_LOG)
    assert "no changes on disk" not in out


# --- poly-9u2 boundary: carried, but never as an exemplar --------------------


def test_the_diff_is_labelled_a_failed_hypothesis():
    out = _msg(diff=_DIFF)
    assert "hypothesis that has now failed" in out
    assert "not a foundation you have to defend" in out


def test_the_retry_is_told_it_may_abandon_the_work():
    out = _msg(diff=_DIFF)
    assert "not obliged to build on it" in out
    assert "genuinely different" in out


def test_stopping_with_an_explanation_is_offered_as_a_good_outcome():
    """One measured job ran three attempts to a 0-byte diff before the
    fourth wrote anything. Thrash is the failure mode this counters."""
    out = _msg(diff=_DIFF)
    assert "Patch Log and stop" in out
    assert "success, not a give-up" in out


def test_the_closing_does_not_reference_a_diff_that_is_not_there():
    assert "That diff is" not in _msg(diff="", log=[_LOG[0]])
    assert "The approach above is" in _msg(diff="", log=[_LOG[0]])


# --- the harness reads the diff, it does not ask the model for it ------------


def test_an_unreadable_diff_never_breaks_the_retry():
    """A retry must not die assembling its own preamble."""
    with patch("dportsv3.agent.worker.emit_diff", side_effect=RuntimeError("boom")):
        assert al._current_diff("e", "o") is None


def test_a_failed_emit_diff_result_reads_as_unavailable_not_empty():
    with patch("dportsv3.agent.worker.emit_diff",
               return_value={"ok": False, "diff": ""}):
        assert al._current_diff("e", "o") is None


def test_a_clean_tree_reads_as_empty_not_unavailable():
    with patch("dportsv3.agent.worker.emit_diff",
               return_value={"ok": True, "diff": ""}):
        assert al._current_diff("e", "o") == ""


# --- the wiring: does any of this actually reach attempt 2? ------------------


class _Usage:
    def __init__(self, billable=0):
        self.billable_tokens = billable
        self.total_tokens = billable
        self.cached_tokens = 0

    def add(self, other):
        self.billable_tokens += other.billable_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens


class _Tier:
    max_tokens = 600_000
    max_iterations = 2


def _run_two_attempts(monkeypatch):
    """Drive run() for two attempts, returning attempt 2's messages."""
    seen: list[list] = []

    class _Response:
        text = "no verified fix"
        tool_calls: list = []

    def fake_run(messages, **kw):
        seen.append(list(messages))
        on_event = kw.get("on_event")
        if on_event is not None:
            on_event({
                "type": "tool_call", "attempt": len(seen), "turn": 1,
                "tool": "get_file",
                "args": {"path": "ports/devel/glib20/overlay.dops"},
                "result": {"ok": True},
            })
            on_event({
                "type": "tool_call", "attempt": len(seen), "turn": 2,
                "tool": "dsynth_build", "args": {"origin": "devel/glib20"},
                "result": {"ok": False, "rebuild_ok": False,
                           "stderr_tail": "ERROR: unsupported platform"},
            })
            on_event({"type": "loop_stop", "reason": "turn_cap"})
        return (_Response(), _Usage(1_000), False)

    monkeypatch.setattr(al.tool_loop, "run", fake_run)
    monkeypatch.setattr(al, "Usage", _Usage)
    monkeypatch.setattr("dportsv3.agent.worker.emit_diff",
                        lambda *a, **k: {"ok": True, "diff": _DIFF})
    al.run("payload", tier=_Tier(), env="env", model="m", origin="devel/glib20")
    assert len(seen) == 2, "the second attempt never ran"
    return seen[1]


def test_attempt_one_gets_no_handoff(monkeypatch):
    """Nothing to hand over, and a phantom one would be a lie."""
    class _R:
        text = ""
        tool_calls: list = []
    captured: list = []
    monkeypatch.setattr(al.tool_loop, "run",
                        lambda m, **k: (captured.append(list(m)), (_R(), _Usage(1), False))[1])
    monkeypatch.setattr(al, "Usage", _Usage)
    al.run("payload", tier=type("T", (), {"max_tokens": 0, "max_iterations": 1})(),
           env="env", model="m")
    assert len(captured[0]) == 2  # system + user payload, nothing else


def test_the_tool_log_reaches_attempt_two(monkeypatch):
    """The helper can be perfect and still be dead code if run() never
    threads the observed calls into the next message."""
    body = _run_two_attempts(monkeypatch)[-1]["content"]
    assert "ports/devel/glib20/overlay.dops" in body
    assert "ERROR: unsupported platform" in body


def test_the_surviving_diff_reaches_attempt_two(monkeypatch):
    body = _run_two_attempts(monkeypatch)[-1]["content"]
    assert "+USES=meson" in body


def test_the_stop_reason_reaches_attempt_two(monkeypatch):
    body = _run_two_attempts(monkeypatch)[-1]["content"]
    assert "ran out of tool turns" in body


def test_attempt_two_does_not_inherit_the_raw_transcript(monkeypatch):
    """Each attempt still starts from [system, user] plus one handoff --
    extending the prior history is what melts the budget by attempt 3."""
    msgs = _run_two_attempts(monkeypatch)
    assert len(msgs) == 3
    assert [m["role"] for m in msgs] == ["system", "user", "user"]


def test_a_truncated_list_says_it_is_truncated():
    """A list read as exhaustive when it is not sends the model looking
    for the entries it thinks are missing."""
    log = [{"tool": "get_file", "args": {"path": f"f{i}"}, "result": {}}
           for i in range(40)]
    out = _msg(diff=_DIFF, log=log)
    assert "… and 20 more" in out


def test_a_short_list_claims_no_truncation():
    log = [{"tool": "get_file", "args": {"path": f"f{i}"}, "result": {}}
           for i in range(3)]
    assert "more" not in _msg(diff=_DIFF, log=log).split("## What survived")[0]


# --- grep is carried as a pattern, not as a path -----------------------------


def test_a_grep_is_carried_with_its_pattern():
    """Exact-arg grep repeats are rare because the model rewords the
    pattern; carrying the pattern is what stops that."""
    log = [{"tool": "grep", "args": {"pattern": "glib-2.86.4", "path": "/work"},
            "result": {"ok": True}}]
    out = _msg(diff=_DIFF, log=log)
    assert "glib-2.86.4" in out
    assert "Searched for:" in out


def test_a_grep_is_not_listed_as_a_file_that_was_read():
    """grep's path is usually a whole tree — listing /work under
    "Looked at" implies a file was read."""
    log = [{"tool": "grep", "args": {"pattern": "p", "path": "/work"},
            "result": {"ok": True}}]
    out = _msg(diff=_DIFF, log=log)
    assert "Looked at:" not in out


# --- a truncated diff says so ------------------------------------------------


def test_a_long_diff_is_truncated_visibly():
    """A diff that ends mid-hunk and reads as complete is worse than
    one that admits it was cut."""
    with patch("dportsv3.agent.worker.emit_diff",
               return_value={"ok": True, "diff": "x" * 9000}):
        out = al._current_diff("e", "o")
    assert "diff truncated" in out
    assert len(out) < 9000


def test_a_short_diff_is_not_marked_truncated():
    with patch("dportsv3.agent.worker.emit_diff",
               return_value={"ok": True, "diff": "short"}):
        assert al._current_diff("e", "o") == "short"
