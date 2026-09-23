"""Unit tests for PatchEventDispatcher.

Phase 5 Substep 3a. Exercises the routing logic that decides which
activity_log entry to emit per event type, the env-suspicious tool-
result force-invalidate, and the trace accumulation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dportsv3.agent.steps import PatchEventDispatcher


# --- helpers ------------------------------------------------------------------


class _LogRecorder:
    """Capture activity_log invocations as (stage, message, extra)."""

    def __init__(self):
        self.entries: list[tuple[str, str, dict | None]] = []

    def __call__(self, queue_root, stage, message,
                 job_id=None, duration_ms=None, extra=None):
        self.entries.append((stage, message, dict(extra) if extra else None))


def _make_dispatcher(**overrides):
    log = overrides.pop("activity_log", _LogRecorder())
    looks_env = overrides.pop("looks_env_suspicious", lambda res: False)
    invalidate_calls = overrides.pop("_invalidate_calls", [])
    invalidate = overrides.pop("invalidate_health_cache",
                               lambda: invalidate_calls.append(True))
    summarize = overrides.pop("summarize_tool_call",
                              lambda tool, args, res: f"summary({tool})")
    d = PatchEventDispatcher(
        queue_root=Path("/tmp/x"),
        job_id="job-1",
        origin="devel/foo",
        activity_log=log,
        looks_env_suspicious=looks_env,
        invalidate_health_cache=invalidate,
        summarize_tool_call=summarize,
        **overrides,
    )
    # Attach recorders for test convenience.
    d._log = log
    d._invalidate_calls = invalidate_calls
    return d


# --- event routing -----------------------------------------------------------


def test_attempt_start_logs_one_row():
    d = _make_dispatcher()
    d({"type": "attempt_start",
       "attempt": 1, "iterations": 3,
       "tokens_used_so_far": 0, "budget": 30000})
    stages = [e[0] for e in d._log.entries]
    assert stages == ["attempt_start"]
    msg = d._log.entries[0][1]
    assert "attempt 1/3" in msg
    assert "devel/foo" in msg
    # Cumulative billable, not this attempt's, and not the total: the
    # word "tokens" alone meant three different things (poly-cwi).
    assert "billable so far 0/30000" in msg


def test_attempt_end_logs_one_row_with_rebuild_ok():
    d = _make_dispatcher()
    d({"type": "attempt_end", "attempt": 1, "rebuild_ok": True,
       "tokens": 1234, "billable_tokens": 200})
    stages = [e[0] for e in d._log.entries]
    assert stages == ["attempt_end"]
    msg = d._log.entries[0][1]
    assert "attempt 1 for devel/foo" in msg
    assert "rebuild_ok=True" in msg
    assert "billable=200 total=1234" in msg


def test_attempt_end_without_billable_says_total_rather_than_guessing():
    """A trace older than billable_tokens has only the total. Printing it
    under "billable" would be the mislabelling poly-cwi exists to stop, so
    the term is dropped instead."""
    d = _make_dispatcher()
    d({"type": "attempt_end", "attempt": 1, "rebuild_ok": False,
       "tokens": 1234})
    msg = d._log.entries[0][1]
    assert "total=1234" in msg
    assert "billable" not in msg


def test_tool_call_logs_with_tool_prefixed_stage_and_summary():
    d = _make_dispatcher(
        summarize_tool_call=lambda tool, args, res: f"path={args.get('path')}",
    )
    d({"type": "tool_call",
       "tool": "put_file",
       "args": {"path": "ports/foo/Makefile"},
       "result": {"ok": True},
       "attempt": 1, "turn": 3, "duration_ms": 17})
    assert d._log.entries[0][0] == "tool:put_file"
    assert d._log.entries[0][1] == "path=ports/foo/Makefile"
    extra = d._log.entries[0][2]
    assert extra["attempt"] == 1
    assert extra["turn"] == 3
    assert extra["ok"] is True


def test_tool_call_with_failure_result_records_ok_false():
    d = _make_dispatcher()
    d({"type": "tool_call",
       "tool": "dsynth_build",
       "args": {"origin": "devel/foo"},
       "result": {"ok": False},
       "attempt": 1, "turn": 5})
    assert d._log.entries[0][2]["ok"] is False


def test_unknown_event_type_no_log_no_crash():
    d = _make_dispatcher()
    d({"type": "weather_changed"})
    assert d._log.entries == []
    # But it still lands in trace_events.
    assert d.trace_events == [{"type": "weather_changed"}]


# --- env-suspicious tool results --------------------------------------------


def test_env_suspicious_tool_result_invalidates_cache():
    d = _make_dispatcher(looks_env_suspicious=lambda res: True)
    d({"type": "tool_call",
       "tool": "materialize_dports",
       "args": {"origin": "devel/foo"},
       "result": {"ok": False, "stderr_tail": "missing DragonFly packages"}})
    assert d._invalidate_calls == [True]
    # Also emits the health_recheck_forced log row + the tool log row.
    stages = [e[0] for e in d._log.entries]
    assert "health_recheck_forced" in stages
    assert "tool:materialize_dports" in stages


def test_env_suspicious_ignored_for_non_tool_events():
    """The env-suspicious check only runs on tool_call events; an
    attempt_end with a "stderr" key isn't reinterpreted as a tool
    failure."""
    d = _make_dispatcher(looks_env_suspicious=lambda res: True)
    d({"type": "attempt_end", "attempt": 1,
       "rebuild_ok": False, "tokens": 100})
    assert d._invalidate_calls == []


def test_invalidate_exception_does_not_break_dispatch():
    """If the cache invalidate raises, the dispatcher still logs +
    accumulates the event."""
    def raising_invalidate():
        raise RuntimeError("invalidate-boom")
    d = _make_dispatcher(
        looks_env_suspicious=lambda res: True,
        invalidate_health_cache=raising_invalidate,
    )
    d({"type": "tool_call", "tool": "x", "args": {}, "result": {"ok": False}})
    assert len(d.trace_events) == 1
    stages = [e[0] for e in d._log.entries]
    assert "health_recheck_forced" in stages
    assert "tool:x" in stages


# --- trace accumulation ------------------------------------------------------


def test_trace_events_accumulate_in_order():
    d = _make_dispatcher()
    d({"type": "attempt_start", "attempt": 1})
    d({"type": "tool_call", "tool": "a", "args": {}, "result": {"ok": True}})
    d({"type": "tool_call", "tool": "b", "args": {}, "result": {"ok": True}})
    d({"type": "attempt_end", "attempt": 1})
    types = [e["type"] for e in d.trace_events]
    assert types == ["attempt_start", "tool_call", "tool_call", "attempt_end"]


def test_trace_starts_empty():
    d = _make_dispatcher()
    assert d.trace_events == []


def test_trace_separated_across_instances():
    """Two dispatcher instances don't share their trace_events list
    (mutable defaults bug guard)."""
    d1 = _make_dispatcher()
    d2 = _make_dispatcher()
    d1({"type": "attempt_start", "attempt": 1})
    assert d1.trace_events != []
    assert d2.trace_events == []


# --- tool_start (poly-qqx9.2) ------------------------------------------------


def test_tool_start_logs_its_own_row_with_the_pairing_id():
    """A tool is visible when it is dispatched, not only when it returns."""
    d = _make_dispatcher()
    d({"type": "tool_start", "attempt": 2, "turn": 7,
       "tool": "dsynth_test", "call_id": "call-abc"})
    stages = [e[0] for e in d._log.entries]
    assert stages == ["tool_start"]
    stage, message, extra = d._log.entries[0]
    assert "dsynth_test" in message
    assert extra == {"attempt": 2, "turn": 7, "tool": "dsynth_test",
                     "call_id": "call-abc", "args": {}}


def test_tool_start_and_completion_share_a_call_id():
    """The pair is matched by call_id, not by adjacency."""
    d = _make_dispatcher()
    d({"type": "tool_start", "attempt": 1, "turn": 3,
       "tool": "dsynth_build", "call_id": "call-1"})
    d({"type": "tool_call", "attempt": 1, "turn": 3, "tool": "dsynth_build",
       "call_id": "call-1", "args": {"origin": "devel/foo"},
       "result": {"ok": True}, "duration_ms": 2480000})
    start, done = d._log.entries
    assert start[0] == "tool_start"
    assert done[0] == "tool:dsynth_build"
    assert start[2]["call_id"] == done[2]["call_id"] == "call-1"


def test_tool_start_is_kept_in_the_trace():
    """tool_trace.jsonl is the post-hoc record; the new event belongs in it."""
    d = _make_dispatcher()
    d({"type": "tool_start", "attempt": 1, "turn": 1,
       "tool": "grep", "call_id": "c1"})
    assert [e["type"] for e in d.trace_events] == ["tool_start"]


# --- tool args on the row (poly-qqx9.13) -------------------------------------


def test_tool_call_row_carries_args_as_fields():
    d = _make_dispatcher()
    d({"type": "tool_call", "attempt": 1, "turn": 2, "tool": "write_file",
       "call_id": "c1", "args": {"path": "files/patch-CMakeLists.txt"},
       "result": {"ok": True}, "duration_ms": 118})
    _stage, _msg, extra = d._log.entries[0]
    assert extra["args"] == {"path": "files/patch-CMakeLists.txt"}


def test_a_file_body_never_lands_in_the_row():
    """put_file's `content` is the whole file; the row stores a measurement."""
    body = "x" * 50000
    d = _make_dispatcher()
    d({"type": "tool_call", "attempt": 1, "turn": 2, "tool": "put_file",
       "call_id": "c1",
       "args": {"path": "ports/devel/llvm19/overlay.dops", "content": body},
       "result": {"ok": True}, "duration_ms": 12})
    _stage, _msg, extra = d._log.entries[0]
    args = extra["args"]
    assert args["path"] == "ports/devel/llvm19/overlay.dops"
    assert body not in str(args)
    assert "50000 chars" in args["content"]
    assert len(str(args)) < 2000


def test_args_beyond_the_dict_budget_are_named_not_silently_missing():
    d = _make_dispatcher()
    args = {f"k{i}": "y" * 150 for i in range(20)}
    d({"type": "tool_call", "attempt": 1, "turn": 1, "tool": "edit_file",
       "call_id": "c1", "args": args, "result": {"ok": True},
       "duration_ms": 1})
    _stage, _msg, extra = d._log.entries[0]
    assert extra["args"]["_dropped"]
    assert len(str(extra["args"])) < 2500


def test_tool_start_carries_the_same_redacted_args():
    d = _make_dispatcher()
    d({"type": "tool_start", "attempt": 3, "turn": 7, "tool": "dsynth_test",
       "call_id": "c9", "args": {"origin": "devel/llvm19", "flavor": "default"}})
    _stage, _msg, extra = d._log.entries[0]
    assert extra["args"] == {"origin": "devel/llvm19", "flavor": "default"}


# --- the model's own sentence on the row (poly-qqx9.13) ----------------------


def test_llm_turn_row_carries_a_capped_excerpt_of_the_text():
    d = _make_dispatcher()
    d({"type": "llm_turn", "attempt": 3, "turn": 7,
       "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
       "tools_requested": ["grep"],
       "text": "Hunk #2 rejected — the upstream file moved under the patch.\n\n"
               "Re-reading CMakeLists.txt around the tablegen block."})
    _stage, _msg, extra = d._log.entries[0]
    assert extra["text"].startswith("Hunk #2 rejected")
    # newlines collapsed, so the card gets one readable line
    assert "\n" not in extra["text"]


def test_a_long_turn_does_not_copy_the_conversation_onto_the_row():
    d = _make_dispatcher()
    d({"type": "llm_turn", "attempt": 1, "turn": 1, "tools_requested": [],
       "text": "word " * 5000})
    _stage, _msg, extra = d._log.entries[0]
    assert len(extra["text"]) <= 301
    assert extra["text"].endswith("…")


def test_an_llm_turn_without_text_is_unchanged():
    """Every job that already ran has no text; that must not look broken."""
    d = _make_dispatcher()
    d({"type": "llm_turn", "attempt": 1, "turn": 1, "tools_requested": []})
    _stage, _msg, extra = d._log.entries[0]
    assert "text" not in extra
